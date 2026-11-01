"""Prepare only the bundled, synthetic Teleop software benchmark for P1 tests."""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import unittest


FIXTURE = Path(__file__).resolve().parent / "fixtures/assessment-software"
EVIDENCE_ROOT = None


def preserve_workspace(test_id, workspace):
    """Keep actual test artifacts before unittest removes its temporary inputs."""
    if EVIDENCE_ROOT is not None and workspace.is_dir():
        shutil.copytree(workspace, EVIDENCE_ROOT / test_id / "work", symlinks=True)


def selected_runtime():
    location = os.environ.get("TELEOP_ASSESSMENT_PYTHON")
    if not location:
        raise unittest.SkipTest("TELEOP_ASSESSMENT_PYTHON is required for the native software benchmark")
    python = Path(location)
    if not python.is_absolute() or not python.is_file():
        raise ValueError("The fixed assessment interpreter must be an existing absolute file")
    from importlib.resources import files

    from description_pipeline.dynamics_assessment.contracts import RUNTIME_SCHEMA, RuntimePin
    from description_pipeline.simulation.runtime import load_runtime_contract

    path = Path(str(files("description_pipeline").joinpath("simulation/teleop-runtime.json")))
    record = load_runtime_contract(path, consumer="assessment")
    del record["schema_version"]
    return python, RuntimePin({"schema": RUNTIME_SCHEMA, **record})


def prepare_plan(destination, python, runtime):
    """Bind a new test plan to the approved asset through Teleop's sole verifier."""
    plan = json.loads((FIXTURE / "plan.template.json").read_bytes())
    if plan["qualification_scope"] != "software_regression_only" or plan["production_engineering_qualification"]:
        raise ValueError("Only the bundled synthetic software benchmark may enter the P1 fixture")
    expected = {
        "package": plan["controller"]["actuator_model"]["package"],
        "artifact": runtime.record["actuator_model"],
    }
    result = subprocess.run(
        [str(python), "-I", "-B", str(Path(__file__).resolve())],
        input=json.dumps(expected), text=True, capture_output=True, timeout=30, check=False,
    )
    if result.returncode:
        raise ValueError("The approved software actuator identity was rejected: " + result.stdout + result.stderr)
    identity = json.loads(result.stdout)
    plan["controller"]["actuator_model"].update(identity)
    destination.mkdir()
    shutil.copytree(FIXTURE / plan["input_closure"]["root"], destination / "inputs")
    plan["input_closure"]["root"] = "inputs"
    plan.pop("_plan_path", None)
    return plan


if __name__ == "__main__":
    # This interpreter has Teleop and actuator-model, without the pipeline.
    from teleop.evaluation.continuous_dynamics import verify_actuator_model_identity

    print(json.dumps(verify_actuator_model_identity(json.load(sys.stdin)), sort_keys=True))
