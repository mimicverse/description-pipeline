"""The offline examples must keep working with the tool in this checkout.

``examples/demo-arm`` (primitive geometry) and ``examples/mesh-arm`` (binary STL meshes) are the only
documented way to exercise the pipeline without CAD, so a stale example would be worse than none.
These tests re-lock each workspace to the checked-out tool, rebuild it from the frozen fixture
snapshot and require the qualification, the workspace layout and the URDF contract audit to pass.
"""

import importlib.metadata
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from description_pipeline.build import assess, build, freeze, lock_toolchain
from description_pipeline.repository import check_layout

ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = {"demo-arm": ROOT / "examples" / "demo-arm", "mesh-arm": ROOT / "examples" / "mesh-arm"}
EXAMPLE = EXAMPLES["demo-arm"]
IGNORED = shutil.ignore_patterns("build", ".venv", "__pycache__")


class DemoExampleTests(unittest.TestCase):
    def test_demo_workspace_builds_and_qualifies(self):
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary) / "demo-arm"
            shutil.copytree(
                EXAMPLE,
                workspace,
                ignore=shutil.ignore_patterns("build", ".venv", "__pycache__"),
            )
            lock_toolchain(workspace)
            freeze(workspace)
            report = build(workspace, "kinematics")
            self.assertTrue(report["passed"], report["blockers"] + [report.get("diagnostic_path", "")])
            check = assess(workspace, "kinematics")
            self.assertTrue(check["passed"], check["blockers"])
            self.assertEqual(check["qualified_for"], ["kinematics"])
            self.assertTrue(check_layout(workspace, "model")["passed"])

    def test_every_example_builds_and_qualifies(self):
        for name, source in EXAMPLES.items():
            with self.subTest(example=name), tempfile.TemporaryDirectory() as temporary:
                workspace = Path(temporary) / name
                shutil.copytree(source, workspace, ignore=IGNORED)
                lock_toolchain(workspace)
                freeze(workspace)
                report = build(workspace, "kinematics")
                self.assertTrue(report["passed"], report["blockers"] + [report.get("diagnostic_path", "")])
                check = assess(workspace, "kinematics")
                self.assertTrue(check["passed"], check["blockers"])
                self.assertEqual(check["qualified_for"], ["kinematics"])
                self.assertTrue(check_layout(workspace, "model")["passed"])

    def test_kinematics_build_and_check_need_mujoco(self):
        """Without the extra MuJoCo provides, the consumer step blocks build and check.

        ``description doctor`` reports the extra as a warning and must say what it is for; this pins
        that wording to what the pipeline really does.  Generating the URDF and MJCF still works, so
        the failure has to name the consumer step rather than the whole profile.
        """

        real_distribution = importlib.metadata.distribution

        def distribution(name: str):
            if name.lower() == "mujoco":
                raise importlib.metadata.PackageNotFoundError(name)
            return real_distribution(name)

        with tempfile.TemporaryDirectory() as temporary, mock.patch.dict(sys.modules, {"mujoco": None}):
            workspace = Path(temporary) / "demo-arm"
            shutil.copytree(EXAMPLE, workspace, ignore=IGNORED)
            with mock.patch("importlib.metadata.distribution", side_effect=distribution):
                lock_toolchain(workspace)
                freeze(workspace)
                built = build(workspace, "kinematics")
                check = assess(workspace, "kinematics")
            self.assertTrue((workspace / "urdf/robot.urdf").is_file())
            self.assertTrue((workspace / "mjcf/scene.xml").is_file())
        self.assertFalse(built["passed"])
        self.assertEqual(built["blockers"], ["consumer.available"])
        self.assertFalse(check["passed"])
        self.assertIn("consumer.available", check["blockers"])

    def test_example_sources_are_labelled_fixture_evidence(self):
        lock = (EXAMPLE / "sources" / "source.lock.json").read_text(encoding="utf-8")
        self.assertIn('"evidence_class": "fixture"', lock)
        self.assertIn('"provider": "fixture"', lock)

    def test_example_satisfies_the_repository_urdf_contract(self):
        """The repository audit reads config/joint_names.yaml; the shipped example must pass it."""

        for name, source in EXAMPLES.items():
            with self.subTest(example=name), tempfile.TemporaryDirectory() as temporary:
                workspace = Path(temporary) / name
                shutil.copytree(source, workspace, ignore=IGNORED)
                result = subprocess.run(
                    [
                        sys.executable,
                        str(ROOT / "tools" / "audit.py"),
                        "--root",
                        str(workspace),
                        "--policy",
                        "strict",
                    ],
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                )
                self.assertEqual(result.returncode, 0, (result.stdout or "") + (result.stderr or ""))
                self.assertIn("0 error", result.stdout)


if __name__ == "__main__":
    unittest.main()
