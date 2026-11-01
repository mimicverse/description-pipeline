"""Run the required optional-assessment software checks without accepting skips."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import unittest


TEST_NAMES = (
    "tests.v1.test_dynamics_assessment_runtime.NativeRuntimeGateTests",
    "tests.v1.test_dynamics_assessment_native.NativeDatabaseTests",
    "tests.v1.test_dynamics_assessment_airflow.AirflowDatabaseTests",
)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("tests", nargs="*", help="Optional unittest names; the default runs the native P1 cases")
    args = parser.parse_args(argv)
    location = os.environ.get("TELEOP_ASSESSMENT_PYTHON")
    if not location:
        parser.error("TELEOP_ASSESSMENT_PYTHON is required for the native assessment suite")
    python = Path(location)
    if not python.is_absolute() or not python.is_file():
        parser.error("The fixed assessment interpreter must be an existing absolute file")
    try:
        suite = unittest.defaultTestLoader.loadTestsFromNames(args.tests or TEST_NAMES)
    except unittest.SkipTest as error:
        parser.exit(1, "The required assessment software suite must not skip selected tests: " + str(error) + "\n")
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    if result.skipped:
        parser.exit(1, "The required assessment software suite must not skip selected tests\n")
    return 0 if result.wasSuccessful() and result.testsRun else 1


if __name__ == "__main__":
    raise SystemExit(main())
