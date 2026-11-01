"""Inspect a selected native environment through its existing stdin gate."""

from __future__ import annotations

import base64
import hashlib
from importlib import metadata
import json
from pathlib import Path
import sys

RUNTIME_IDENTITY_SCHEMA = "solidworks-to-urdf.teleop-runtime-identity/v1"
RUNTIME_CONTRACT_SCHEMA = "solidworks-to-urdf.teleop-runtime/v1"


def installed_runtime(names: list[str]) -> dict:
    """Read installed RECORDs and native versions after parent registration."""
    import mujoco
    import teleop

    prefix = Path(sys.prefix).resolve()
    packages = {}
    for name in names:
        distribution = metadata.distribution(name)
        rows = []
        for member in distribution.files or []:
            if member.hash is None:
                continue
            path = distribution.locate_file(member).resolve()
            if not path.is_relative_to(prefix):
                raise ValueError("Runtime dependency escapes its installation")
            actual = hashlib.new(member.hash.mode, path.read_bytes()).digest()
            if base64.urlsafe_b64encode(actual).decode().rstrip("=") != member.hash.value:
                raise ValueError("Runtime dependency bytes differ from RECORD")
            rows.append((str(member), actual.hex()))
        if not rows:
            raise ValueError("Runtime dependency has no verifiable inventory")
        packages[name] = {
            "version": distribution.version,
            "files_sha256": hashlib.sha256(json.dumps(sorted(rows)).encode()).hexdigest(),
        }
        if name in {"robot-teleop", "actuator-model"}:
            origin = json.loads(distribution.read_text("direct_url.json") or "{}")
            packages[name]["archive_sha256"] = origin.get("archive_info", {}).get("hashes", {}).get("sha256")
    root = Path(teleop.__file__).resolve().parent
    if not root.is_relative_to(prefix):
        raise ValueError("Teleop must be installed from a fixed noneditable wheel")
    files = {
        "teleop/" + path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in root.rglob("*")
        if path.is_file() and "__pycache__" not in path.parts
    }
    try:
        metadata.distribution("actuator-model")
    except metadata.PackageNotFoundError:
        actuator = None
    else:
        if "actuator-model" not in names:
            raise ValueError("PICO runtime must not install actuator-model")
        import actuator_model

        actuator = actuator_model.package_info()
    return {
        "schema_version": RUNTIME_IDENTITY_SCHEMA,
        "packages": packages,
        "teleop_files": files,
        "engine": mujoco.mj_versionString(),
        "actuator_model": actuator,
    }


def validate_runtime_identity(identity: dict, contract: dict) -> dict:
    if identity.get("schema_version") != RUNTIME_IDENTITY_SCHEMA:
        raise ValueError("Runtime identity schema is unsupported")
    if {name: item["version"] for name, item in identity["packages"].items()} != contract["dependency_versions"]:
        raise ValueError("Runtime dependencies differ from the fixed consumer closure")
    if identity["engine"] != contract["dependency_versions"]["mujoco"]:
        raise ValueError("Native MuJoCo engine differs from the fixed consumer closure")
    if identity["teleop_files"] != contract["files"]:
        raise ValueError("Installed Teleop package differs from the fixed source wheel")
    if identity["packages"]["robot-teleop"].get("archive_sha256") != contract["wheel_sha256"]:
        raise ValueError("Installed Teleop archive differs from the fixed source wheel")
    actuator = identity["actuator_model"]
    if contract["actuator_model"] is None:
        if actuator is not None:
            raise ValueError("PICO runtime must not install actuator-model")
    elif (
        not isinstance(actuator, dict)
        or actuator.get("version") != contract["dependency_versions"]["actuator-model"]
        or actuator.get("editable") is not False
    ):
        raise ValueError("Assessment requires the fixed noneditable actuator-model wheel")
    elif identity["packages"]["actuator-model"].get("archive_sha256") != contract["actuator_model"]["wheel_sha256"]:
        raise ValueError("Installed actuator-model archive differs from the fixed consumer asset")
    elif (
        actuator.get("git_commit") is not None and actuator["git_commit"] != contract["actuator_model"]["source_commit"]
    ):
        raise ValueError("Installed actuator-model commit conflicts with the fixed consumer asset")
    return {
        **{key: value for key, value in identity.items() if key != "teleop_files"},
        "source_commit": contract["source_commit"],
        "wheel_sha256": contract["wheel_sha256"],
        "actuator_model_asset": contract["actuator_model"],
    }


def verify_runtime_contract(contract: dict) -> dict:
    """Inspect and verify the explicit consumer projection in this interpreter.

    Native callers may load this standalone standard-library bootstrap with
    runpy after validating its release bytes and passing their resource gate.
    """
    if (
        not isinstance(contract, dict)
        or contract.get("schema_version") != RUNTIME_CONTRACT_SCHEMA
        or set(contract)
        != {
            "schema_version",
            "source_commit",
            "source_tree",
            "wheel_filename",
            "wheel_sha256",
            "files",
            "dependency_versions",
            "actuator_model",
        }
    ):
        raise ValueError("An explicit current consumer runtime projection is required")
    return validate_runtime_identity(installed_runtime(list(contract["dependency_versions"])), contract)


if __name__ == "__main__":
    # The supervisor captures and persists the process identity before sending.
    print(json.dumps(installed_runtime(json.load(sys.stdin))))
