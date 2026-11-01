"""Schema identities owned by the pipeline's interactive consumer."""

from __future__ import annotations

import json
from pathlib import Path, PurePosixPath
import re

from .runtime import RUNTIME_CONTRACT_SCHEMA

TELEOP_RUNTIME_SCHEMA = RUNTIME_CONTRACT_SCHEMA
TELEOP_RECORDING_SCHEMA = "solidworks-to-urdf.teleop-recording/v1"
RECORDING_VERIFICATION_SCHEMA = "solidworks-to-urdf.recording-verification/v1"


def _hex(value, length):
    return isinstance(value, str) and re.fullmatch(f"[0-9a-f]{{{length}}}", value) is not None


def _wheel(record, package, version):
    name = record.get("wheel_filename")
    if (
        not _hex(record.get("source_commit"), 40)
        or not _hex(record.get("wheel_sha256"), 64)
        or not isinstance(name, str)
        or not name.startswith(package.replace("-", "_") + "-" + version + "-")
        or not name.endswith(".whl")
        or "/" in name
        or "\\" in name
    ):
        raise ValueError("Runtime wheel identity is invalid")


def load_runtime_contract(path: Path, *, consumer: str) -> dict:
    """Validate the common inventory once and project one explicit consumer."""
    if consumer not in {"pico", "assessment"}:
        raise ValueError("Runtime consumer must be pico or assessment")
    contract = json.loads(path.read_bytes())
    if (
        not isinstance(contract, dict)
        or set(contract) != {"schema_version", "common", "consumers"}
        or contract.get("schema_version") != TELEOP_RUNTIME_SCHEMA
    ):
        raise ValueError("Teleop runtime contract schema is unsupported")
    common, consumers = contract["common"], contract["consumers"]
    if (
        not isinstance(common, dict)
        or set(common) != {"source_commit", "source_tree", "wheel_filename", "wheel_sha256", "files"}
        or not _hex(common["source_tree"], 40)
        or not isinstance(consumers, dict)
        or set(consumers) != {"pico", "assessment"}
    ):
        raise ValueError("Runtime common inventory or consumer set is invalid")
    files = common["files"]
    if not isinstance(files, dict) or not files:
        raise ValueError("Runtime common file inventory is required")
    for name, sha in files.items():
        if (
            not isinstance(name, str)
            or not name.startswith("teleop/")
            or "\\" in name
            or ".." in PurePosixPath(name).parts
            or PurePosixPath(name).as_posix() != name
            or not _hex(sha, 64)
        ):
            raise ValueError("Runtime common file identity is invalid")
    for role, entry in consumers.items():
        if not isinstance(entry, dict) or set(entry) != {"dependency_versions", "actuator_model"}:
            raise ValueError("Runtime consumer identity is invalid")
        versions = entry["dependency_versions"]
        if not isinstance(versions, dict) or not {"robot-teleop", "mujoco"} <= versions.keys():
            raise ValueError("Runtime dependency closure is missing")
        for name, version in versions.items():
            if (
                not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", name)
                or not isinstance(version, str)
                or not re.fullmatch(r"[0-9][A-Za-z0-9.!+_-]{0,127}", version)
            ):
                raise ValueError("Runtime dependency identity is invalid")
        _wheel(common, "robot-teleop", versions["robot-teleop"])
        asset = entry["actuator_model"]
        if role == "pico":
            if asset is not None or "actuator-model" in versions or versions["mujoco"].split(".")[:2] != ["3", "13"]:
                raise ValueError("PICO requires its MuJoCo 3.13 closure without actuator-model")
        else:
            if (
                not isinstance(asset, dict)
                or set(asset) != {"source_commit", "wheel_filename", "wheel_sha256"}
                or "actuator-model" not in versions
                or versions["mujoco"].split(".")[:2] != ["3", "10"]
            ):
                raise ValueError("Assessment requires its MuJoCo 3.10 actuator-model closure")
            _wheel(asset, "actuator-model", versions["actuator-model"])
    return {"schema_version": contract["schema_version"], **common, **consumers[consumer]}
