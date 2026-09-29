"""Resolve an asset commit's tool lock using only trusted main and the standard library."""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path


def resolve(model: Path, tooling: Path, candidate: str, profile: str) -> dict[str, str]:
    if not re.fullmatch(r"[0-9a-f]{40}", candidate) or not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", profile):
        raise ValueError("Exact model SHA and a safe profile identifier required")
    actual = subprocess.check_output(["git", "-C", str(model), "rev-parse", "HEAD"], text=True).strip()
    if actual != candidate:
        raise ValueError("Checked out model differs from dispatched SHA")
    path = model / "config/toolchain.lock.json"
    if path.is_symlink() or not path.resolve().is_relative_to(model.resolve()):
        raise ValueError("Escaped model tool lock")
    lock = json.loads(path.read_text(encoding="utf-8"))
    commit = lock.get("source_commit", "")
    version = lock.get("version", "")
    if lock.get("development") is not False or not re.fullmatch(r"[0-9a-f]{40}", commit or ""):
        raise ValueError("Central validation requires a committed tool release")
    if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", version):
        raise ValueError("Invalid tool version")
    command = ["git", "-C", str(tooling)]
    subprocess.run([*command, "merge-base", "--is-ancestor", commit, "origin/main"], check=True)
    if not re.fullmatch(r"[0-9a-f]{64}", lock.get("package_digest", "")):
        raise ValueError("Tool lock requires the installed package content digest")
    return {"tool_sha": commit, **environment(lock)}


def environment(lock: dict) -> dict[str, str]:
    """Select only supported hosted runners; a model never supplies a runner label or command."""
    platform = lock.get("platform", {})
    supported = {
        ("Linux", "x86_64"): ("ubuntu-24.04", "requirements/linux-py312.lock"),
        ("Windows", "AMD64"): (
            "windows-2022",
            "src/description_pipeline/sources/solidworks/deploy/requirements/win-py312.lock",
        ),
    }
    if not isinstance(platform, dict) or platform.get("implementation") != "CPython":
        raise ValueError("Model requires a supported CPython environment")
    if not all(isinstance(platform.get(key), str) for key in ("system", "machine")):
        raise ValueError("Model platform must declare system and machine strings")
    key = (platform.get("system"), platform.get("machine"))
    if key not in supported:
        raise ValueError("Model platform must be Linux x86_64 or Windows AMD64")
    python = lock.get("python", "")
    if not isinstance(python, str) or not re.fullmatch(r"3\.12\.[0-9]{1,2}", python):
        raise ValueError("Model requires an exact supported Python 3.12 patch version")
    runner, requirements = supported[key]
    return {"runner": runner, "python": python, "requirements": requirements}


if __name__ == "__main__":
    resolved = resolve(Path("model"), Path("driver"), os.environ["MODEL_SHA"], os.environ["PROFILE"])
    resolved["driver_sha"] = subprocess.check_output(["git", "-C", "driver", "rev-parse", "HEAD"], text=True).strip()
    with Path(os.environ["GITHUB_OUTPUT"]).open("a", encoding="utf-8") as stream:
        for key, value in resolved.items():
            stream.write(f"{key}={value}\n")
