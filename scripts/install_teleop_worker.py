"""Install an explicit isolated Teleop consumer or lightweight PICO link."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
import venv


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--role", choices=("pico", "assessment", "pico-link"), required=True)
    parser.add_argument("--wheel", type=Path, help="Fixed Teleop wheel for pico or assessment")
    parser.add_argument("--actuator-wheel", type=Path, help="Fixed actuator-model wheel for assessment")
    parser.add_argument("--destination", type=Path, required=True)
    parser.add_argument(
        "--wheelhouse", type=Path, help="Use this offline wheel directory instead of the package registry"
    )
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    if sys.version_info[:2] != (3, 12) or sys.platform != "linux":
        parser.error("The private environments require Linux and CPython 3.12")
    assets = []
    if args.role in {"pico", "assessment"}:
        if args.wheel is None:
            parser.error("The selected consumer requires --wheel")
        sys.path.insert(0, str(root / "src"))
        from description_pipeline.simulation.schemas import load_runtime_contract
        from description_pipeline.simulation.runtime import validate_runtime_identity

        contract = load_runtime_contract(
            root / "src/description_pipeline/simulation/teleop-runtime.json", consumer=args.role
        )
        assets.append(
            ("robot-teleop[evaluation]" if args.role == "assessment" else "robot-teleop", args.wheel, contract)
        )
        if args.role == "assessment":
            if args.actuator_wheel is None:
                parser.error("The assessment role requires --actuator-wheel")
            assets.append(("actuator-model[mujoco]", args.actuator_wheel, contract["actuator_model"]))
        elif args.actuator_wheel is not None:
            parser.error("The PICO role does not install actuator-model")
    elif args.wheel is not None or args.actuator_wheel is not None:
        parser.error("The PICO link role does not install model wheels")
    snapshots = []
    for requirement, path, expected in assets:
        path = path.resolve()
        payload = path.read_bytes()
        if path.name != expected["wheel_filename"] or hashlib.sha256(payload).hexdigest() != expected["wheel_sha256"]:
            parser.error("A supplied model wheel differs from the fixed consumer contract")
        snapshots.append((requirement, path.name, payload, expected["wheel_sha256"]))
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
            "--only-binary=:all:",
        ]
        if args.role in {"pico", "assessment"}:
            staging = Path(temporary) / "wheels"
            staging.mkdir()
            requirement = Path(temporary) / "wheel.lock"
            rows = []
            for name, filename, payload, sha in snapshots:
                path = staging / filename
                path.write_bytes(payload)
                rows.append(f"{name} @ {path.as_uri()} --hash=sha256:{sha}")
            requirement.write_text("\n".join(rows) + "\n")
            command += ["-r", str(root / f"requirements/teleop-{args.role}-py312.lock"), "-r", str(requirement)]
        else:
            command += ["-r", str(root / "requirements/pico-link-py312.lock")]
        if args.wheelhouse:
            command += ["--no-index", "--find-links", str(args.wheelhouse.resolve())]
        subprocess.run(command, check=True)
    subprocess.run([str(python), "-m", "pip", "check"], check=True)
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
        probe = subprocess.run(
            [str(python), "-I", "-B", str(root / "src/description_pipeline/simulation/runtime.py")],
            input=json.dumps(list(contract["dependency_versions"])),
            capture_output=True,
            text=True,
            check=True,
        )
        identity = validate_runtime_identity(json.loads(probe.stdout), contract)
        print(json.dumps({"role": args.role, "python": str(python), "runtime": identity}))


if __name__ == "__main__":
    main()
