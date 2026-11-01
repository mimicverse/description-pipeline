"""Install the fixed Teleop wheel into a fresh private Python 3.12 environment."""

from __future__ import annotations

import argparse
import hashlib
from pathlib import Path
import subprocess
import sys
import tempfile
import venv


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wheel", type=Path, required=True)
    parser.add_argument("--destination", type=Path, required=True)
    parser.add_argument(
        "--wheelhouse", type=Path, help="Use this offline wheel directory instead of the package registry"
    )
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(root / "src"))
    from description_pipeline.simulation.schemas import load_runtime_contract

    contract = load_runtime_contract(root / "src/description_pipeline/simulation/teleop-runtime.json")
    if sys.version_info[:2] != (3, 12) or sys.platform != "linux":
        parser.error("The worker requires Linux and CPython 3.12")
    wheel = args.wheel.resolve()
    if (
        wheel.name != contract["wheel_filename"]
        or hashlib.sha256(wheel.read_bytes()).hexdigest() != contract["wheel_sha256"]
    ):
        parser.error("The supplied Teleop wheel differs from the fixed integration contract")
    if not args.destination.is_absolute() or args.destination.exists() or args.destination.is_symlink():
        parser.error("The destination must be a fresh absolute path")
    venv.EnvBuilder(with_pip=True).create(args.destination)
    with tempfile.TemporaryDirectory(prefix="teleop-install-") as temporary:
        requirement = Path(temporary) / "wheel.lock"
        requirement.write_text(f"robot-teleop @ {wheel.as_uri()} --hash=sha256:{contract['wheel_sha256']}\n")
        command = [
            str(args.destination / "bin/python"),
            "-m",
            "pip",
            "install",
            "--require-hashes",
            "-r",
            str(root / "requirements/teleop-worker-py312.lock"),
            "-r",
            str(requirement),
        ]
        if args.wheelhouse:
            command += ["--no-index", "--find-links", str(args.wheelhouse.resolve())]
        subprocess.run(command, check=True)
    print(f"Teleop worker installed: {args.destination / 'bin/python'}")


if __name__ == "__main__":
    main()
