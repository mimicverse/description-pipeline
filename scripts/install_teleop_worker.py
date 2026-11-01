"""Install a private Teleop worker or lightweight PICO link environment."""

from __future__ import annotations

import argparse
import hashlib
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
import venv


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--role", choices=("worker", "pico-link"), default="worker")
    parser.add_argument("--wheel", type=Path, help="Fixed Teleop wheel; required for the worker role")
    parser.add_argument("--destination", type=Path, required=True)
    parser.add_argument(
        "--wheelhouse", type=Path, help="Use this offline wheel directory instead of the package registry"
    )
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    if sys.version_info[:2] != (3, 12) or sys.platform != "linux":
        parser.error("The private environments require Linux and CPython 3.12")
    if args.role == "worker":
        if args.wheel is None:
            parser.error("The worker role requires --wheel")
        sys.path.insert(0, str(root / "src"))
        from description_pipeline.simulation.schemas import load_runtime_contract

        contract = load_runtime_contract(root / "src/description_pipeline/simulation/teleop-runtime.json")
        wheel = args.wheel.resolve()
        if (
            wheel.name != contract["wheel_filename"]
            or hashlib.sha256(wheel.read_bytes()).hexdigest() != contract["wheel_sha256"]
        ):
            parser.error("The supplied Teleop wheel differs from the fixed integration contract")
    elif args.wheel is not None:
        parser.error("The PICO link role does not install a Teleop wheel")
    if not args.destination.is_absolute() or args.destination.exists() or args.destination.is_symlink():
        parser.error("The destination must be a fresh absolute path")
    venv.EnvBuilder(with_pip=True).create(args.destination)
    python = args.destination / "bin/python"
    with tempfile.TemporaryDirectory(prefix="teleop-install-") as temporary:
        command = [
            str(python),
            "-m",
            "pip",
            "install",
            "--require-hashes",
        ]
        if args.role == "worker":
            requirement = Path(temporary) / "wheel.lock"
            requirement.write_text(f"robot-teleop @ {wheel.as_uri()} --hash=sha256:{contract['wheel_sha256']}\n")
            command += ["-r", str(root / "requirements/teleop-worker-py312.lock"), "-r", str(requirement)]
        else:
            command += ["--only-binary=:all:", "-r", str(root / "requirements/pico-link-py312.lock")]
        if args.wheelhouse:
            command += ["--no-index", "--find-links", str(args.wheelhouse.resolve())]
        subprocess.run(command, check=True)
    if args.role == "pico-link":
        script = args.destination / "libexec/pico_pipeline_link.py"
        script.parent.mkdir()
        shutil.copyfile(root / "scripts/pico_pipeline_link.py", script)
        launcher = args.destination / "bin/pico-link"
        with launcher.open("x", encoding="utf-8") as stream:
            stream.write(f'#!/bin/sh\nexec {shlex.quote(str(python))} -I -B {shlex.quote(str(script))} "$@"\n')
        launcher.chmod(0o755)
        print(f"PICO link installed. Start a session with: {launcher} /path/to/pico-link-session.json")
    else:
        print(f"Teleop worker installed: {python}")


if __name__ == "__main__":
    main()
