"""Qualify local simulation measurements by execution, never by a claimed identity."""

from __future__ import annotations

import tempfile
from pathlib import Path

from ..io import PipelineError, digest


def verify_replay(root: Path, record: dict, subject: str, profile: dict) -> dict:
    """Replay with the locked tool and compare every stable measurement exactly.

    No host name, key or candidate-supplied trust policy grants authority. The trust
    boundary is the verifier installed by the consumer; promotion also requires its
    source commit to have been accepted on main. Physical evidence cannot be replayed
    this way, and this verifier never grants training or hardware qualification.
    """
    try:
        from .. import __version__
        from ..build import subject_files, verify_toolchain
        from .simulation import ACCEPTANCE_CONFIG, CONTROLLER_ID, execute_tests

        if profile["purpose"] != "simulation" or record.get("purpose") != "simulation":
            raise ValueError("Local replay only qualifies the declared simulation use")
        if record.get("attestation") != {"kind": "local_replay", "schema_version": "description.local-replay/v1"}:
            raise ValueError("Unsupported local replay record")
        if (
            record.get("schema_version") != "description.acceptance/v2"
            or record.get("subject") != subject
            or record.get("profile_digest") != digest(profile)
            or record.get("environment") != profile["consumer_environment"]
            or digest(subject_files(root)) != subject
        ):
            raise ValueError("Local acceptance identity or environment differs from the candidate")
        if record.get("tool") != {
            "package_version": __version__,
            "controller": CONTROLLER_ID,
            "identity": verify_toolchain(root),
        }:
            raise ValueError("Local acceptance was measured with a different tool or environment")
        if record["model"]["config"] != ACCEPTANCE_CONFIG:
            raise ValueError("Local acceptance requires the canonical declared experiment")
        if any(
            record["qualification"].get(key) is not False
            for key in ("physical_calibration", "training_qualified", "hardware_qualified", "release_qualified")
        ):
            raise ValueError("Simulation measurements cannot claim physical or release qualification")
        with tempfile.TemporaryDirectory(prefix="description-acceptance-replay-") as temporary:
            measured = execute_tests(root, profile, Path(record["model"]["config"]), Path(temporary))
            from ..io import file_digest

            expected_model = {
                "mjcf": measured["config"]["mjcf"],
                "mjcf_sha256": file_digest(measured["mjcf_path"]),
                "mjcf_includes": measured["includes"],
                "config": measured["config_file"].relative_to(root).as_posix(),
                "config_sha256": file_digest(measured["config_file"]),
                "subject_files": len(subject_files(root)),
            }
            if any(record["model"].get(key) != value for key, value in expected_model.items()):
                raise ValueError("Local acceptance model or experiment differs from the replay")
            expected_runtime = {**measured["runtime"], "python": measured["measured"]["python"], "mujoco_warnings": []}
            if record.get("runtime") != expected_runtime or measured["environment"] != record["environment"]:
                raise ValueError("Local acceptance runtime differs from the replay")
            results = record["results"]
            if not isinstance(results, list) or len(results) != len(measured["results"]):
                raise ValueError("Local acceptance suite coverage differs from the replay")
            for stored, actual in zip(results, measured["results"], strict=True):
                # Timestamps describe the original execution; all measured values,
                # conditions, thresholds and compressed telemetry must reproduce.
                if not isinstance(stored, dict) or not stored.get("executed_at"):
                    raise ValueError("Invalid local acceptance result")
                stable = {key: value for key, value in stored.items() if key != "executed_at"}
                replayed = {key: value for key, value in actual.items() if key != "executed_at"}
                if stable != replayed or actual["passed"] is not True:
                    raise ValueError("Local measurements or telemetry do not reproduce; rerun acceptance")
        if digest(subject_files(root)) != subject:
            raise ValueError("Model inputs changed during acceptance replay")
        return {"trusted": True, "status": "passed", "execution": "local_replay", "suites": len(results)}
    except ImportError as error:
        return {"trusted": False, "status": "not_run", "execution": "local_replay", "reason": str(error)}
    except (PipelineError, OSError, ValueError, KeyError, TypeError, AttributeError, RuntimeError) as error:
        return {"trusted": False, "status": "failed", "execution": "local_replay", "reason": str(error)}
