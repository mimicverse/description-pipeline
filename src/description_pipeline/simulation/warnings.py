"""Bind bounded warning displays to the original complete certificate inventory."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re

from ..io import canonical, digest
from ..mjcf_contract import projection_warnings
from ..pipeline.contracts import ArtifactRef


def warning_inventory(source: dict) -> dict:
    if "warnings" in source:
        raise ValueError("Inline warning inventories are unsupported; bind the original certificate")
    inventory = source.get("warning_inventory")
    if not isinstance(inventory, dict) or set(inventory) != {
        "certificate", "count", "sha256", "shown", "omitted", "items",
    }:
        raise ValueError("Simulation source requires its bounded warning inventory binding")
    if len(canonical(inventory)) > 32768:
        raise ValueError("Warning display exceeds its bounded descriptor budget")
    if (
        any(type(inventory[key]) is not int or inventory[key] < 0 for key in ("count", "shown", "omitted"))
        or not isinstance(inventory["items"], list)
        or inventory["shown"] != len(inventory["items"])
        or inventory["count"] != inventory["shown"] + inventory["omitted"]
        or not isinstance(inventory["sha256"], str)
        or re.fullmatch(r"[0-9a-f]{64}", inventory["sha256"]) is None
    ):
        raise ValueError("Warning inventory identity or display counts are invalid")
    for item in inventory["items"]:
        if (
            not isinstance(item, dict) or set(item) != {"text", "excerpt"}
            or not isinstance(item["text"], str) or type(item["excerpt"]) is not bool
        ):
            raise ValueError("Warning display items require explicit excerpt metadata")
    certificate = inventory["certificate"]
    if not isinstance(certificate, dict) or set(certificate) != {"artifact", "path", "bytes", "sha256"}:
        raise ValueError("Warning inventory certificate member is invalid")
    ArtifactRef.from_record(certificate["artifact"])
    path = certificate["path"]
    if (
        not isinstance(path, str) or not path or Path(path).is_absolute()
        or Path(path).as_posix() != path or ".." in Path(path).parts or "\\" in path or ":" in path
        or type(certificate["bytes"]) is not int or certificate["bytes"] < 0
        or not isinstance(certificate["sha256"], str)
        or re.fullmatch(r"[0-9a-f]{64}", certificate["sha256"]) is None
    ):
        raise ValueError("Warning inventory certificate identity is invalid")
    return inventory


def bind_warning_source(source: dict) -> dict:
    inventory = warning_inventory(source)
    certificate = inventory["certificate"]
    files = dict(source["files"])
    if certificate["path"] in files and files[certificate["path"]] != certificate["sha256"]:
        raise ValueError("Warning certificate conflicts with the model source inventory")
    files[certificate["path"]] = certificate["sha256"]
    return {**source, "files": files}


def verify_warning_inventory(source: dict, root: Path) -> dict:
    inventory = warning_inventory(source)
    certificate = inventory["certificate"]
    path = (root / certificate["path"]).resolve()
    if not path.is_relative_to(root.resolve()):
        raise ValueError("Warning certificate escapes the materialized source")
    raw = path.read_bytes()
    if len(raw) != certificate["bytes"] or hashlib.sha256(raw).hexdigest() != certificate["sha256"]:
        raise ValueError("Warning certificate differs from its original member binding")
    records = projection_warnings(json.loads(raw))
    if len(records) != inventory["count"] or digest(records) != inventory["sha256"]:
        raise ValueError("Warning inventory differs from its original certificate")
    return {"count": len(records), "sha256": digest(records), "certificate": certificate}
