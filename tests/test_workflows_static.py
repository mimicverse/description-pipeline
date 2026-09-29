"""Static audit of ``.github/workflows``: Actions cannot start, so nothing else would check them.

Every command, local path, job reference, step output and action reference in the workflows has to
resolve in this checkout.  A workflow that names a removed subcommand or a deleted script fails
silently today (the job never runs), which is worse than a red build, so the checker is exercised
with one negative control per class of drift it claims to catch.
"""

from __future__ import annotations

import functools
import re
import subprocess
import sys
import unittest
from collections.abc import Callable
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github/workflows"
CLI = [sys.executable, "-m", "description_pipeline"]
SHA = re.compile(r"^[0-9a-f]{40}$")
#: `name==version` as a workflow writes it into a `pip install` line.
PIN = re.compile(r"(?<![\w.-])([A-Za-z0-9][A-Za-z0-9._-]*)==([0-9][^\s\"']*)")
#: The files that own this repository's pins: a workflow may install them, not decide them.
CANONICAL_PINS = (
    "pyproject.toml",
    "requirements/linux-py312.lock",
    "requirements/win-py312-dev.lock",
    "src/description_pipeline/sources/solidworks/deploy/requirements/win-py312.lock",
)


def canonical_pins() -> dict[str, str]:
    """Name -> version for everything this repository pins, as the pinning files state it."""

    pins: dict[str, str] = {}
    for name in CANONICAL_PINS:
        for match in PIN.finditer((ROOT / name).read_text(encoding="utf-8")):
            pins.setdefault(match.group(1).lower().replace("_", "-"), match.group(2))
    return pins


def workflow_pin_problems(texts: dict[str, str], canonical: dict[str, str]) -> list[str]:
    """Findings for workflows that install a version the repository pins differently.

    Nobody runs them while Actions cannot start, which is exactly how four of them kept installing
    `setuptools==82.0.1` for two releases after CVE-2026-59890 moved the pin to 84.0.0.
    """

    problems: list[str] = []
    for name, text in sorted(texts.items()):
        for match in PIN.finditer(text):
            package = match.group(1).lower().replace("_", "-")
            expected = canonical.get(package)
            if expected and expected != match.group(2):
                problems.append(
                    f"{name}: installs {match.group(1)}=={match.group(2)}, but this repository pins {expected}"
                )
    return problems


#: A pre-commit hook repository for `<tool>` is usually named `<tool>-pre-commit` and tagged `v<version>`.
HOOK_REPO = re.compile(r"repo:\s*https://github\.com/[^/\s]+/([A-Za-z0-9._-]+?)-pre-commit\s*\n\s*rev:\s*v?(\S+)")


def hook_pin_problems(text: str, canonical: dict[str, str]) -> list[str]:
    """Findings for a pre-commit hook that runs a different version than the gate does.

    A hook wired to another ``ruff`` formats and fixes with rules the gate then rejects — the same
    two-tools-two-answers problem as a workflow that installs a superseded pin.
    """

    problems: list[str] = []
    for match in HOOK_REPO.finditer(text):
        package = match.group(1).lower().replace("_", "-")
        expected = canonical.get(package)
        if expected and expected != match.group(2):
            problems.append(f"pre-commit runs {package}=={match.group(2)}, but this repository pins {expected}")
    return problems


#: Every ecosystem GitHub accepts in ``dependabot.yml`` (the point is to catch a typo, not to be
#: exhaustive forever: if this repository starts using a new one, add it here).
ECOSYSTEMS = {
    "bundler",
    "cargo",
    "composer",
    "devcontainers",
    "docker",
    "docker-compose",
    "dotnet-sdk",
    "elm",
    "gitsubmodule",
    "github-actions",
    "gomod",
    "gradle",
    "helm",
    "hex",
    "maven",
    "mix",
    "npm",
    "nuget",
    "pip",
    "pub",
    "swift",
    "terraform",
}
INTERVALS = {"daily", "weekly", "monthly", "quarterly", "semiannually", "yearly", "cron"}


def dependabot_problems(text: str) -> list[str]:
    """Findings for a Dependabot config that would silently stop updating anything.

    Dependabot is not GitHub Actions: it keeps running while Actions are disabled, and it fails
    quietly — a misspelled key or a missing schedule means no pull requests, not a red build.  The
    three bumps it opened on 2026-09-20 (#36–#38) are the only reason anyone would notice.
    """

    problems: list[str] = []
    try:
        data = yaml.safe_load(text) or {}
    except yaml.YAMLError as error:
        return [f"dependabot.yml is not YAML: {error}"]
    if not isinstance(data, dict):
        return ["dependabot.yml must be a mapping"]
    if data.get("version") != 2:
        problems.append("dependabot.yml must declare version: 2")
    updates = data.get("updates")
    if not isinstance(updates, list) or not updates:
        problems.append("dependabot.yml has no updates entries")
        return problems
    for index, entry in enumerate(updates):
        if not isinstance(entry, dict):
            problems.append(f"updates[{index}] is not a mapping")
            continue
        if entry.get("package-ecosystem") not in ECOSYSTEMS:
            problems.append(f"updates[{index}] has an unknown package-ecosystem: {entry.get('package-ecosystem')!r}")
        if not isinstance(entry.get("directory"), str) or not entry.get("directory"):
            problems.append(f"updates[{index}] needs a directory")
        schedule = entry.get("schedule")
        if not isinstance(schedule, dict) or schedule.get("interval") not in INTERVALS:
            problems.append(f"updates[{index}] needs a schedule interval in {sorted(INTERVALS)}")
    return problems


@functools.cache
def help_text(arguments: tuple[str, ...]) -> tuple[int, str]:
    result = subprocess.run([*CLI, *arguments, "--help"], cwd=ROOT, capture_output=True, text=True, encoding="utf-8")
    # argparse prints usage for invalid choices too, so the exit code is what proves the command.
    return result.returncode, (result.stdout or "") + (result.stderr or "")


def reusable_outputs(path: Path) -> set[str]:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    # YAML parses the key `on` as True; accept both spellings.
    triggers = data.get("on", data.get(True, {})) or {}
    if not isinstance(triggers, dict):
        return set()
    outputs = (triggers.get("workflow_call") or {}).get("outputs") or {}
    return set(outputs) if isinstance(outputs, dict) else set()


class Auditor:
    """Collect problems for one workflow file; ``checked`` counts the references examined."""

    def __init__(self, help_command: Callable[[tuple[str, ...]], tuple[int, str]] = help_text) -> None:
        self.help_command = help_command
        self.problems: list[str] = []
        self.checked = 0
        #: Directories a workflow creates by checking out another repository: paths under them
        #: resolve in CI even though they do not exist in this checkout.
        self.checked_out: set[str] = set()

    def local_path(self, where: str, token: str) -> None:
        token = token.strip("'\"")
        if not token or token.startswith("-") or "${{" in token or "*" in token:
            return
        if any(token == prefix or token.startswith(f"{prefix}/") for prefix in self.checked_out):
            return
        self.checked += 1
        if not (ROOT / token).exists():
            self.problems.append(f"{where}: referenced path does not exist: {token}")

    def cli(self, where: str, tokens: list[str]) -> None:
        subcommands: list[str] = []
        for token in tokens:
            if token.startswith("-"):
                break
            if re.fullmatch(r"[a-z][a-z-]*", token):
                subcommands.append(token)
            else:
                break
        if not subcommands:
            return
        self.checked += 1
        code, text = self.help_command(tuple(subcommands))
        if code != 0 or "usage:" not in text:
            self.problems.append(f"{where}: `description {' '.join(subcommands)}` is not a real command")

    def run_block(self, where: str, script: str) -> None:
        for raw in script.splitlines():
            line = raw.strip().split("  #")[0].rstrip()
            tokens = line.split()
            if not tokens or line.startswith(("#", "|")):
                continue
            head = tokens[0]
            # Shell scaffolding that is not a command.
            if head in {"cd", "set", "echo", "export", "if", "fi", "for", "done", "then", "else"}:
                continue
            if head == "description":
                self.cli(where, tokens[1:])
            elif head == "python" and len(tokens) > 2 and tokens[1] == "-m":
                if tokens[2].startswith("description_pipeline"):
                    self.cli(where, tokens[3:])
                elif tokens[2] == "pip":
                    self.pip(where, tokens)
            elif head == "pip":
                self.pip(where, tokens)
            else:
                handed_to_an_interpreter = self.script_path(tokens)
                if handed_to_an_interpreter is not None:
                    self.local_path(where, handed_to_an_interpreter)

    @staticmethod
    def script_path(tokens: list[str]) -> str | None:
        """The script a ``run:`` line hands to an interpreter, when the line looks like one."""

        if tokens[0] == "python" and len(tokens) > 1 and tokens[1].endswith(".py"):
            return tokens[1]
        if tokens[0] in {"bash", "sh", "source", "."} and len(tokens) > 1:
            return tokens[1]
        if tokens[0].startswith("./") or tokens[0].endswith((".py", ".sh")):
            return tokens[0]
        return None

    def pip(self, where: str, tokens: list[str]) -> None:
        for index, token in enumerate(tokens):
            if token in {"-r", "--requirement", "--find-links", "-f", "--dest", "-d"} and index + 1 < len(tokens):
                self.local_path(where, tokens[index + 1])

    def document(self, name: str, data: dict) -> None:
        self.checked_out = set()
        jobs = data.get("jobs") or {}
        for step_source in jobs.values():
            if not isinstance(step_source, dict):
                continue
            for step in step_source.get("steps", []):
                if isinstance(step, dict) and str(step.get("uses", "")).startswith("actions/checkout"):
                    path = (step.get("with") or {}).get("path")
                    if isinstance(path, str) and "${{" not in path:
                        self.checked_out.add(path)
        for job_id, job in jobs.items():
            if not isinstance(job, dict):
                self.problems.append(f"{name}/{job_id}: job is not a mapping")
                continue
            needs = job.get("needs", [])
            for need in needs if isinstance(needs, list) else [needs]:
                self.checked += 1
                if need not in jobs:
                    self.problems.append(f"{name}/{job_id}: needs '{need}' which is not a job in this file")
            reusable = job.get("uses")
            if isinstance(reusable, str) and reusable.startswith("./"):
                self.local_path(f"{name}/{job_id}", reusable[2:])
            step_ids = {step["id"] for step in job.get("steps", []) if isinstance(step, dict) and "id" in step}
            for step in job.get("steps", []):
                if not isinstance(step, dict):
                    continue
                where = f"{name}/{job_id}/{step.get('name', step.get('uses', 'step'))}"
                if isinstance(step.get("run"), str):
                    self.run_block(where, step["run"])
                uses = step.get("uses")
                if isinstance(uses, str):
                    self.checked += 1
                    if uses.startswith("./"):
                        self.local_path(where, uses[2:])
                    elif not uses.startswith("docker://") and "@" in uses:
                        repo, _, ref = uses.partition("@")
                        if not SHA.fullmatch(ref):
                            self.problems.append(f"{where}: action {repo} is not pinned to a full SHA ({ref})")
                for key, value in {**(step.get("with") or {}), **(step.get("env") or {})}.items():
                    if not isinstance(value, str) or "${{ steps." not in value:
                        continue
                    match = re.search(r"steps\.([A-Za-z0-9_-]+)\.outputs", value)
                    if match:
                        self.checked += 1
                        if match.group(1) not in step_ids:
                            self.problems.append(
                                f"{where}: {key} reads steps.{match.group(1)}.outputs but that step id "
                                f"is not in {job_id}"
                            )
            for source, output in re.findall(r"needs\.([A-Za-z0-9_-]+)\.outputs\.([A-Za-z0-9_-]+)", str(job)):
                self.checked += 1
                target = jobs.get(source)
                if target is None:
                    self.problems.append(f"{name}/{job_id}: reads outputs of unknown job '{source}'")
                elif str(target.get("uses", "")).startswith("./"):
                    declared = reusable_outputs(ROOT / target["uses"][2:])
                    if output not in declared:
                        self.problems.append(
                            f"{name}/{job_id}: job '{source}' does not declare output '{output}' ({sorted(declared)})"
                        )


class WorkflowStaticTests(unittest.TestCase):
    def test_workflows_install_the_versions_this_repository_pins(self):
        texts = {path.name: path.read_text(encoding="utf-8") for path in sorted(WORKFLOWS.glob("*.yml"))}
        self.assertEqual(workflow_pin_problems(texts, canonical_pins()), [])

    def test_the_pre_commit_hook_runs_the_version_the_gate_runs(self):
        text = (ROOT / ".pre-commit-config.yaml").read_text(encoding="utf-8")
        self.assertEqual(hook_pin_problems(text, canonical_pins()), [])

    def test_a_hook_on_another_version_is_reported(self):
        """The control: a hook one minor version behind formats what the gate then rejects."""

        text = (ROOT / ".pre-commit-config.yaml").read_text(encoding="utf-8").replace("rev: v0.16.8", "rev: v0.16.7")
        self.assertEqual(
            hook_pin_problems(text, canonical_pins()),
            ["pre-commit runs ruff==0.16.7, but this repository pins 0.16.8"],
        )

    def test_a_workflow_that_pins_another_version_is_reported(self):
        """The control: four workflows installed setuptools==82.0.1 for two releases, unnoticed."""

        text = (
            (WORKFLOWS / "validate.yml").read_text(encoding="utf-8").replace("setuptools==84.0.0", "setuptools==82.0.1")
        )
        self.assertEqual(
            workflow_pin_problems({"validate.yml": text}, canonical_pins()),
            ["validate.yml: installs setuptools==82.0.1, but this repository pins 84.0.0"],
        )

    def test_the_dependabot_config_would_actually_update_something(self):
        """Dependabot keeps running while Actions do not, and it reports nothing when it breaks."""

        self.assertEqual(dependabot_problems((ROOT / ".github/dependabot.yml").read_text(encoding="utf-8")), [])

    def test_a_dependabot_config_that_stops_updating_is_reported(self):
        """The controls: a typo, a missing directory and a missing schedule each disable it quietly."""

        broken = "version: 2\nupdates:\n  - package-ecosystem: github-action\n    directory: /"
        self.assertEqual(
            dependabot_problems(broken),
            [
                "updates[0] has an unknown package-ecosystem: 'github-action'",
                f"updates[0] needs a schedule interval in {sorted(INTERVALS)}",
            ],
        )
        self.assertEqual(
            dependabot_problems("version: 1\nupdates: []"),
            ["dependabot.yml must declare version: 2", "dependabot.yml has no updates entries"],
        )

    def test_every_workflow_reference_resolves(self):
        files = sorted(WORKFLOWS.glob("*.yml"))
        self.assertGreaterEqual(len(files), 5, "the workflows should be discovered")
        auditor = Auditor()
        for path in files:
            with self.subTest(workflow=path.name):
                auditor.document(path.name, yaml.safe_load(path.read_text(encoding="utf-8")) or {})
        self.assertGreater(auditor.checked, 50, "workflow scan looks broken: too few references found")
        self.assertEqual(auditor.problems, [], "workflow 里引用的命令/路径必须在本检出里存在")

    def test_each_class_of_drift_is_rejected(self):
        planted = {
            "missing path": "jobs:\n  a:\n    steps:\n      - run: python tools/does-not-exist.py\n",
            "bogus CLI subcommand": "jobs:\n  a:\n    steps:\n      - run: python -m description_pipeline frobnicate\n",
            "unknown needs": "jobs:\n  a:\n    needs: nope\n    steps:\n      - run: echo hi\n",
            "unpinned action": "jobs:\n  a:\n    steps:\n      - uses: actions/checkout@v4\n",
            "unknown step output": (
                "jobs:\n  a:\n    steps:\n"
                "      - name: use\n        run: echo hi\n"
                "        env:\n          X: ${{ steps.nowhere.outputs.value }}\n"
            ),
            "missing reusable output": (
                "jobs:\n  a:\n    steps:\n      - run: echo ${{ needs.b.outputs.nope }}\n"
                "  b:\n    uses: ./.github/workflows/model-environment.yml\n"
            ),
        }
        for label, text in planted.items():
            with self.subTest(case=label):
                auditor = Auditor()
                auditor.document("planted.yml", yaml.safe_load(text) or {})
                self.assertTrue(auditor.problems, f"{label} was not detected")


if __name__ == "__main__":
    unittest.main()
