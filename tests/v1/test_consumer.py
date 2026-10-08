"""Consumer isolation, byte binding and fail-closed runtime readiness."""

from __future__ import annotations

import copy
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from description_pipeline import solidworks
from description_pipeline.verification import consumer
from description_pipeline.sources.solidworks.discovery import prepare_native_package

URDF = (
    '<robot name="test"><link name="base_link"><inertial><mass value="1"/>'
    '<inertia ixx="1" ixy="0" ixz="0" iyy="1" iyz="0" izz="1"/></inertial></link></robot>'
)


class ConsumerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.valid = consumer.readiness()

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        (self.root / "urdf").mkdir()
        (self.root / "meshes").mkdir()
        (self.root / "urdf/robot.urdf").write_text(URDF)
        self.report = {**copy.deepcopy(self.valid), "inputs": consumer._inputs(self.root)}

    def test_actual_loader_uses_delivered_bytes_without_importing_the_parent_consumer(self):
        with patch.dict("sys.modules", {"mujoco": None}):
            report = consumer.load(self.root)
        self.assertEqual(report["inputs"], consumer._inputs(self.root))
        self.assertEqual(report["body_names"], ["base_link"])
        self.assertEqual(report["joint_names"], [])
        self.assertEqual(report["bodies"], 2)
        self.assertEqual(report["joints"], 0)

    def test_child_failure_keeps_diagnostics_and_has_no_retry(self):
        result = subprocess.CompletedProcess([], 1, "", "ImportError: missing native library")
        with (
            patch.object(consumer.subprocess, "run", return_value=result) as run,
            self.assertRaises(consumer.ConsumerError) as caught,
        ):
            consumer.load(self.root)
        run.assert_called_once()
        self.assertIn("-I", run.call_args.args[0])
        self.assertEqual(caught.exception.details["stderr"], result.stderr)
        self.assertEqual(caught.exception.details["returncode"], 1)

    def test_actual_loader_retains_moving_joint_names(self):
        document = URDF.removesuffix("</robot>") + (
            '<link name="arm_link"><inertial><mass value="1"/>'
            '<inertia ixx="1" ixy="0" ixz="0" iyy="1" iyz="0" izz="1"/></inertial></link>'
            '<joint name="shoulder" type="revolute"><parent link="base_link"/><child link="arm_link"/>'
            '<axis xyz="0 0 1"/><limit lower="-1" upper="1" effort="1" velocity="1"/></joint></robot>'
        )
        (self.root / "urdf/robot.urdf").write_text(document)
        report = consumer.load(self.root)
        self.assertEqual(report["body_names"], ["arm_link", "base_link"])
        self.assertEqual(report["joint_names"], ["shoulder"])
        self.assertEqual((report["bodies"], report["joints"]), (3, 1))

    def test_timeout_kills_and_reaps_the_actual_child(self):
        script = self.root / "slow.py"
        script.write_text("import os,sys,time\nprint(os.getpid(),file=sys.stderr,flush=True)\ntime.sleep(10)\n")
        with (
            patch.object(consumer, "__file__", str(script)),
            patch.object(consumer, "TIMEOUT_SECONDS", 0.25),
            self.assertRaises(consumer.ConsumerError) as caught,
        ):
            consumer.load(self.root)
        cause = caught.exception.__cause__
        self.assertIsInstance(cause, subprocess.TimeoutExpired)
        self.assertEqual(caught.exception.details["timeout_seconds"], 0.25)
        if os.name != "nt":
            pid = int(cause.stderr.decode().strip())
            with self.assertRaises(ProcessLookupError):
                os.kill(pid, 0)

    def test_invalid_child_output_cannot_pass(self):
        for output in ("not json", "null", "[]", json.dumps({"reader": "mujoco"})):
            with self.subTest(output=output):
                result = subprocess.CompletedProcess([], 0, output, "")
                with (
                    patch.object(consumer.subprocess, "run", return_value=result),
                    self.assertRaises(consumer.ConsumerError),
                ):
                    consumer.load(self.root)

    def test_hashes_counts_names_and_runtime_version_must_match(self):
        for key, value in (
            ("inputs", {}),
            ("bodies", 3),
            ("bodies", True),
            ("joints", 1),
            ("body_names", ["another_link"]),
            ("joint_names", ["extra_joint"]),
            ("version", "unknown"),
        ):
            with self.subTest(key=key, value=value):
                report = {**self.report, key: value}
                result = subprocess.CompletedProcess([], 0, json.dumps(report), "")
                with (
                    patch.object(consumer.subprocess, "run", return_value=result),
                    self.assertRaises(consumer.ConsumerError),
                ):
                    consumer.load(self.root)

    def test_parent_checks_input_bytes_again_after_loading(self):
        def changed(*args, **kwargs):
            (self.root / "urdf/robot.urdf").write_text(URDF.replace('value="1"', 'value="2"'))
            return subprocess.CompletedProcess([], 0, json.dumps(self.report), "")

        with (
            patch.object(consumer.subprocess, "run", side_effect=changed),
            self.assertRaisesRegex(consumer.ConsumerError, "input hashes"),
        ):
            consumer.load(self.root)

    def test_doctor_fails_when_installed_consumer_cannot_load(self):
        error = consumer.ConsumerError("Consumer loading failed", stderr="native import failed")
        with patch.object(consumer, "readiness", side_effect=error):
            result = solidworks.doctor()
        check = next(row for row in result["checks"] if row["id"] == "consumer.urdf")
        self.assertFalse(result["passed"])
        self.assertFalse(check["passed"])
        self.assertEqual(check["details"]["stderr"], "native import failed")

    def test_unready_consumer_prevents_native_process_creation(self):
        source = self.root / "engineering"
        source.mkdir()
        (source / "robot.SLDASM").write_bytes(b"neutral saved assembly")
        with (
            patch.object(consumer, "readiness", side_effect=consumer.ConsumerError("Consumer loading failed")),
            patch("description_pipeline.sources.solidworks.native.SolidWorksBackend") as native,
            self.assertRaises(consumer.ConsumerError),
        ):
            prepare_native_package(source, self.root / "prepared", "probe")
        native.assert_not_called()


if __name__ == "__main__":
    unittest.main()
