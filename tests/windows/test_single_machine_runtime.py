"""The Windows single-machine runtime: local pipeline plus the collection worker.

The Windows bundle used to carry only the collection worker, so the public pipeline
(build + independent verification) could not run on the same machine.  These checks keep
the runtime complete - MuJoCo and its closure - and keep the shipped launcher local-first.
"""

from __future__ import annotations

import importlib.util
import json
import re
import sys
import tomllib
import unittest
from pathlib import Path
from typing import ClassVar

SRC = Path(__file__).resolve().parents[2] / "src"
if importlib.util.find_spec("description_pipeline") is None and str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from description_pipeline.sources.solidworks import deploy as deploy_resources  # noqa: E402
from tools import audit_dependencies  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
RUNTIME_LOCKS = {
    "requirements/linux-py312.lock": "the Linux bundle runtime",
    "requirements/win-py312-dev.lock": "the Windows development environment",
    "src/description_pipeline/sources/solidworks/deploy/requirements/win-py312.lock": "the packaged worker runtime",
}


def pinned_versions() -> dict[str, str]:
    """Name -> version for the packaged Windows lock."""

    pinned: dict[str, str] = {}
    for line in deploy_resources.resource_path(deploy_resources.LOCK_FILE).read_text(encoding="utf-8").splitlines():
        entry = line.strip()
        if entry and not entry.startswith("#"):
            name, _, version = entry.partition("==")
            pinned[name.lower()] = version
    return pinned


def declared_versions() -> dict[str, str]:
    """Name -> version for what the wheel itself requires (a pip install gets these exact pins)."""

    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    declared: dict[str, str] = {}
    optional = [entry for group in project.get("optional-dependencies", {}).values() for entry in group]
    for entry in [*project.get("dependencies", []), *optional]:
        name, separator, version = str(entry).partition("==")
        if separator:
            declared[audit_dependencies.canonical(name.split(";")[0].strip())] = version
    return declared


def shared_pin_problems(declared: dict[str, str], locks: dict[str, dict[str, str]]) -> list[str]:
    """Every package two sources pin differently, named with both versions and both sources."""

    tables: dict[str, dict[str, str]] = {"the wheel metadata": declared, **locks}
    names = {name for table in tables.values() for name in table}
    problems: list[str] = []
    for name in sorted(names):
        versions = {label: table[name] for label, table in tables.items() if name in table}
        if len(set(versions.values())) > 1:
            detail = ", ".join(f"{label} pins {version}" for label, version in sorted(versions.items()))
            problems.append(f"{name}: {detail}")
    return problems


class WindowsRuntimeLockTests(unittest.TestCase):
    """The bundle must build and verify locally, not only collect CAD."""

    REQUIRED: ClassVar[set[str]] = {
        # local pipeline: build, MuJoCo verification
        "mujoco",
        "numpy",
        "pyyaml",
        "jsonschema",
        "packaging",
        # transitive closure of the above for the offline wheel set
        "absl-py",
        "etils",
        "fsspec",
        "glfw",
        "pyopengl",
        "typing-extensions",
        "zipp",
        # collection worker: COM boundary and schema validation
        "pywin32",
        "attrs",
        "jsonschema-specifications",
        "referencing",
        "rpds-py",
    }

    def test_lock_carries_the_single_machine_runtime(self) -> None:
        pinned = pinned_versions()
        missing = sorted(name for name in self.REQUIRED if name not in pinned)
        self.assertEqual(missing, [], f"Windows lock lacks {missing}")

    def test_mujoco_and_the_com_boundary_are_pinned(self) -> None:
        pinned = pinned_versions()
        self.assertEqual(pinned["mujoco"], "3.13.0")
        self.assertEqual(pinned["pywin32"], "311")

    def test_mujoco_matches_the_linux_consumer_lock(self) -> None:
        linux = (ROOT / "requirements/linux-py312.lock").read_text(encoding="utf-8")
        match = re.search(r"^mujoco==([0-9.]+)$", linux, re.MULTILINE)
        if match is None:
            self.fail("the Linux consumer lock must pin MuJoCo")
        self.assertEqual(pinned_versions()["mujoco"], match.group(1), "Windows and Linux must agree on MuJoCo")

    def test_every_shared_pin_agrees_across_the_four_sources(self) -> None:
        """The wheel, the Linux bundle, the Windows environment and the worker must not drift apart.

        The same machine runs the worker and the public pipeline, and the same wheel is installed into
        both; two versions of numpy in one session is the kind of defect that only shows up as a
        mysterious mass-property difference, so the agreement is enforced instead of inspected.
        """

        locks = {RUNTIME_LOCKS[path]: dict(audit_dependencies.parse_lock(ROOT / path)) for path in RUNTIME_LOCKS}
        self.assertEqual(shared_pin_problems(declared_versions(), locks), [])

    def test_the_shared_pin_check_reports_a_drifted_version(self) -> None:
        """The control: move one pin in one lock and the pair has to be reported."""

        locks = {RUNTIME_LOCKS[path]: dict(audit_dependencies.parse_lock(ROOT / path)) for path in RUNTIME_LOCKS}
        drifted = "the packaged worker runtime"
        locks[drifted]["numpy"] = "2.4.0"
        problems = shared_pin_problems(declared_versions(), locks)
        self.assertEqual(len(problems), 1, problems)
        self.assertIn("numpy", problems[0])
        self.assertIn(f"{drifted} pins 2.4.0", problems[0])
        self.assertIn("the wheel metadata pins", problems[0])

    def test_lock_is_a_runtime_and_not_a_development_environment(self) -> None:
        pinned = pinned_versions()
        for development_tool in ("mypy", "ruff", "build", "wheel", "setuptools"):
            self.assertNotIn(development_tool, pinned, f"{development_tool} is a development tool")

    def test_packaged_accessor_matches_the_lock_file(self) -> None:
        raw = deploy_resources.resource_path(deploy_resources.LOCK_FILE).read_text(encoding="utf-8")
        entries = [line.strip() for line in raw.splitlines() if line.strip() and not line.startswith("#")]
        self.assertEqual(deploy_resources.lock_requirements(), entries)


class LocalFirstLauncherTests(unittest.TestCase):
    """The shipped launcher is local-first with the remote entry kept for compatibility."""

    def test_example_targets_the_local_pipeline(self) -> None:
        payload = json.loads(
            deploy_resources.resource_path(deploy_resources.SUBMIT_TEMPLATE).read_text(encoding="utf-8")
        )
        self.assertNotIn("build_host", payload)
        self.assertNotIn("remote_python", payload)
        self.assertRegex(payload["model_root"], r"^([A-Za-z]:[\\/]|\\\\)")
        self.assertEqual(payload["profile"], "kinematics")
        self.assertTrue(payload["message"].strip())

    def test_launcher_keeps_the_remote_entry_available(self) -> None:
        script = deploy_resources.resource_path(deploy_resources.SUBMIT_SCRIPT).read_text(encoding="utf-8")
        self.assertIn("build_host", script, "the legacy remote mode must stay selectable")
        self.assertIn("'-R'", script, "remote mode still uses one ssh session with a reverse tunnel")
        self.assertIn("'--expect-worker-url'", script, "remote mode still pins the tunnel url")

    def test_launcher_does_not_rewrite_the_source(self) -> None:
        script = deploy_resources.resource_path(deploy_resources.SUBMIT_SCRIPT).read_text(encoding="utf-8")
        for forbidden in ("--worker-host", "--reuse-source", "worker_url", "allow_remote_worker"):
            self.assertNotIn(forbidden, script, f"the launcher must not pass {forbidden}")

    def test_powershell_sources_stay_ascii(self) -> None:
        # Windows PowerShell 5.1 reads BOM-less UTF-8 as ANSI, so a non-ASCII byte breaks the
        # script before it can report anything useful.
        for name in (deploy_resources.SUBMIT_SCRIPT, deploy_resources.WORKER_SCRIPT):
            raw = deploy_resources.resource_path(name).read_bytes()
            self.assertTrue(all(byte < 128 for byte in raw), f"{name} must stay ASCII")


if __name__ == "__main__":
    unittest.main()
