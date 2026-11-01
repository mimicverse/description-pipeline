"""Run the fixed consumer/native profile; missing prerequisites and skips fail."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import sys
import unittest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    if sys.platform != "linux" or not hasattr(os, "pidfd_open") or shutil.which("prlimit") is None:
        parser.error("Linux, pidfd and prlimit are required")
    for name in ("TELEOP_PICO_PYTHON", "TELEOP_ASSESSMENT_PYTHON", "PICO_LINK_PYTHON"):
        value = os.environ.get(name, "")
        path = Path(value)
        if not value or not path.is_absolute() or not path.is_file() or not os.access(path, os.X_OK):
            parser.error(f"{name} must name an explicit absolute installed interpreter")
    for name in ("httpx", "mujoco", "numpy", "pgserver", "psycopg2", "uvicorn", "websockets"):
        if importlib.util.find_spec(name) is None:
            parser.error(f"The platform test environment requires {name}")
    for name in ("teleop", "pinocchio", "actuator_model"):
        if importlib.util.find_spec(name) is not None:
            parser.error(f"The platform test environment must not install {name}")
    names = [
        "tests.v1.test_pico_sessions.PicoSessionTests",
        "tests.v1.test_teleop_runtime.TeleopRuntimeTests.test_real_pico_closure_and_protocol_replay_after_source_relocation",
        "tests.v1.test_teleop_runtime.TeleopRuntimeTests.test_real_assessment_closure_public_controller_and_delay",
        "tests.v1.test_teleop_runtime.TeleopRuntimeTests.test_real_assessment_rejects_coherent_record_stale_record_shadow_and_archive",
    ]
    suite = unittest.defaultTestLoader.loadTestsFromNames(names)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    report = {
        "test_modules": names,
        "runtime_helper_sha256": hashlib.sha256(
            Path(sys.modules["description_pipeline.simulation.runtime"].__file__).read_bytes()
        ).hexdigest(),
        "platform_package": sys.modules["description_pipeline"].__file__,
        "tests_run": result.testsRun,
        "skipped": [str(test) for test, _reason in result.skipped],
        "failures": [str(test) for test, _trace in result.failures],
        "errors": [str(test) for test, _trace in result.errors],
        "pass": result.wasSuccessful() and not result.skipped and result.testsRun > 0,
    }
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, indent=2) + "\n")
    return 0 if report["pass"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
