"""Schema identities owned by the pipeline's interactive consumer."""

from __future__ import annotations

import json
from pathlib import Path

TELEOP_RUNTIME_SCHEMA = "solidworks-to-urdf.teleop-runtime/v1"
TELEOP_RECORDING_SCHEMA = "solidworks-to-urdf.teleop-recording/v1"
RECORDING_VERIFICATION_SCHEMA = "solidworks-to-urdf.recording-verification/v1"


def load_runtime_contract(path: Path) -> dict:
    contract = json.loads(path.read_bytes())
    if not isinstance(contract, dict) or contract.get("schema_version") != TELEOP_RUNTIME_SCHEMA:
        raise ValueError("Teleop runtime contract schema is unsupported")
    return contract
